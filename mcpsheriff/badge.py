"""Render a conformance badge from a mcpsheriff JSON report.

The badge is the distribution mechanism: a maintainer pastes one line into a
README, and the link in that line points back to the project page. The value is
computed from a real report, so the badge cannot claim a score the server did
not earn.

Usage:
    mcpsheriff run --json report.json -- <server command>
    mcpsheriff-badge --report report.json --out badge.svg
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence
from xml.sax.saxutils import escape

DEFAULT_LINK = "https://normahub.cc/mcpsheriff"
GREEN = "#2f855a"
RED = "#c53030"
GREY = "#555555"

# Approximate advance width of Verdana 11px. Exact font metrics would require a
# dependency; a constant is enough because the text is short and fixed.
CHAR_WIDTH = 7
PADDING = 10


def load_summary(path: str) -> Dict[str, int]:
    """Reads the `summary` object written by `mcpsheriff run --json`."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        raise ValueError(f"{path} has no summary object; pass a file written by `--json`")
    required = ("total", "passed", "failed")
    missing = [key for key in required if not isinstance(summary.get(key), int)]
    if missing:
        raise ValueError(f"{path} summary is missing integer field(s): {', '.join(missing)}")
    return summary


def badge_text(summary: Dict[str, int]) -> str:
    total = summary["total"]
    passed = summary["passed"]
    return f"{passed}/{total}"


def badge_color(summary: Dict[str, int]) -> str:
    return GREEN if summary["failed"] == 0 else RED


def _width(text: str) -> int:
    return len(text) * CHAR_WIDTH + PADDING


def render_svg(summary: Dict[str, int], label: str = "MCP conformance") -> str:
    value = badge_text(summary)
    color = badge_color(summary)
    label_width = _width(label)
    value_width = _width(value)
    total_width = label_width + value_width
    label_center = label_width / 2
    value_center = label_width + value_width / 2
    description = f"{label}: {value}"
    # escape() does not touch quotes; the label is user input and lands in an attribute.
    attribute = escape(description, {'"': "&quot;"})

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="20" '
        f'role="img" aria-label="{attribute}">'
        f"<title>{escape(description)}</title>"
        '<linearGradient id="s" x2="0" y2="100%">'
        '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/>'
        "</linearGradient>"
        f'<clipPath id="r"><rect width="{total_width}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r)"><rect width="{label_width}" height="20" fill="{GREY}"/>'
        f'<rect x="{label_width}" width="{value_width}" height="20" fill="{color}"/>'
        f'<rect width="{total_width}" height="20" fill="url(#s)"/></g>'
        '<g fill="#fff" text-anchor="middle" '
        'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{label_center:g}" y="15">{escape(label)}</text>'
        f'<text x="{value_center:g}" y="15">{escape(value)}</text>'
        "</g></svg>\n"
    )


def markdown_snippet(image_path: str, link: str = DEFAULT_LINK, label: str = "MCP conformance",
                     summary: Optional[Dict[str, int]] = None) -> str:
    alt = label if summary is None else f"{label} {badge_text(summary)}"
    return f"[![{alt}]({image_path})]({link})"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcpsheriff-badge",
        description="Render a conformance badge from a mcpsheriff JSON report.",
    )
    parser.add_argument("--report", required=True, metavar="FILE", help="JSON report from `mcpsheriff run --json`")
    parser.add_argument("--out", default="badge.svg", metavar="FILE", help="Where to write the SVG (default badge.svg)")
    parser.add_argument("--label", default="MCP conformance", help="Left-hand text of the badge")
    parser.add_argument("--link", default=DEFAULT_LINK, help=f"Target of the badge link (default {DEFAULT_LINK})")
    args = parser.parse_args(argv)

    try:
        summary = load_summary(args.report)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    svg = render_svg(summary, args.label)
    try:
        Path(args.out).write_text(svg, encoding="utf-8")
    except OSError as error:
        print(f"error: cannot write {args.out}: {error}", file=sys.stderr)
        return 2

    print(f"badge:    {args.out}")
    print("markdown:")
    print(markdown_snippet(args.out, args.link, args.label, summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
