"""Command line interface.

    mcpsheriff run -- npx -y @normahub/mcp-server
    mcpsheriff run --http https://example.com/mcp
    mcpsheriff run -- npx -y @normahub/mcp-server --snapshot mcp.snapshot.json
    mcpsheriff list-checks
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from . import __version__
from .checks import CHECK_ORDER, Context, check_snapshot, initialize, run_checks
from .protocol import HttpSession, ProtocolError, StdioSession
from .report import render_table, summary, summary_line, write_json, write_junit

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

USAGE = """mcpsheriff — test any MCP server

  mcpsheriff run -- <command> [args...]     run checks against a stdio server
  mcpsheriff run --http <url>               run checks against a Streamable HTTP server
  mcpsheriff list-checks                    show check names and order
  mcpsheriff version                        print version

Run options:
  --timeout SECONDS        reply timeout per request (default: 30)
  --latency-budget-ms MS   budget for tools/list (default: 5000)
  --only a,b,c             run only the listed checks
  --snapshot FILE          compare schemas against a saved snapshot
  --update-snapshot        write/refresh the snapshot and pass
  --json FILE              write machine-readable results
  --junit FILE             write JUnit XML for CI
  --quiet                  do not print the per-check table

Exit codes: 0 all checks passed, 1 at least one failed, 2 could not run the server.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcpsheriff", add_help=False)
    parser.add_argument("--http", metavar="URL")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--latency-budget-ms", type=int, default=5000)
    parser.add_argument("--only", default=None)
    parser.add_argument(
        "--fail-on",
        dest="fail_on",
        choices=["critical", "high", "medium", "low"],
        default="high",
        help="lowest severity that makes the run fail (default: high)",
    )
    parser.add_argument(
        "--header",
        dest="headers",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="extra HTTP header for --http, repeatable (e.g. Authorization: Bearer ...)",
    )
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--update-snapshot", action="store_true")
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--junit", dest="junit_path", default=None)
    parser.add_argument("--quiet", action="store_true")
    return parser


def _split_args(argv: Sequence[str]) -> tuple:
    """Everything before `--` is ours, everything after belongs to the server."""
    if "--" in argv:
        index = argv.index("--")
        return list(argv[:index]), list(argv[index + 1 :])
    return list(argv), []


def _parse_headers(values):
    parsed = {}
    for raw in values:
        name, sep, value = raw.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"invalid --header {raw!r}, expected NAME:VALUE")
        parsed[name.strip()] = value.strip()
    return parsed


def _print_results(
    results: List,
    quiet: bool,
    json_path: Optional[str],
    junit_path: Optional[str],
    fail_on: str = "high",
) -> int:
    if not quiet:
        print(render_table(results))
        print()
    print(summary_line(results))

    from .checks import blocking_failures

    stats = summary(results)
    if json_path:
        write_json(results, json_path)
        print(f"json:  {json_path}")
    if junit_path:
        write_junit(results, junit_path)
        print(f"junit: {junit_path}")
    blocking = blocking_failures(results, fail_on)
    if blocking and len(blocking) != stats["failed"]:
        print(f"{len(blocking)} failure(s) at severity {fail_on} or above")
    return EXIT_FAIL if blocking else EXIT_OK


def _run(argv: Sequence[str], parser: argparse.ArgumentParser) -> int:
    options, command = _split_args(argv)

    try:
        args = parser.parse_args(options)
    except SystemExit:
        print(USAGE, file=sys.stderr)
        return EXIT_ERROR

    if bool(args.http) == bool(command):
        print("error: pass either `-- <command>` or `--http <url>`, not both/neither.", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return EXIT_ERROR

    try:
        headers = _parse_headers(args.headers)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR

    only = None
    if args.only:
        only = [name.strip() for name in args.only.split(",") if name.strip()]
        unknown = [name for name in only if name not in CHECK_ORDER]
        if unknown:
            print(f"error: unknown check(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"available: {', '.join(CHECK_ORDER)}", file=sys.stderr)
            return EXIT_ERROR

    if args.update_snapshot and not args.snapshot:
        print("error: --update-snapshot requires --snapshot FILE", file=sys.stderr)
        return EXIT_ERROR

    session = None
    try:
        if args.http:
            session = HttpSession(args.http, timeout=args.timeout, headers=headers)
        else:
            session = StdioSession(command)
            session.start()

        initialize(session, timeout=args.timeout, client_version=__version__)
        context = Context(
            session=session,
            timeout=args.timeout,
            latency_budget_ms=args.latency_budget_ms,
        )
        results = run_checks(context, only=only)

        if args.snapshot:
            snapshot_result = check_snapshot(context, args.snapshot, update=args.update_snapshot)
            if snapshot_result is not None:
                results.append(snapshot_result)

        return _print_results(results, args.quiet, args.json_path, args.junit_path, args.fail_on)

    except ProtocolError as error:
        print(f"error: {error}", file=sys.stderr)
        if session is not None:
            tail = session.stderr_tail()
            if tail:
                print("--- server stderr (tail) ---", file=sys.stderr)
                print(tail, file=sys.stderr)
        return EXIT_ERROR
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - closing must never mask the result
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return EXIT_OK

    command, rest = argv[0], argv[1:]

    if command == "version" or command == "--version":
        print(f"mcpsheriff {__version__}")
        return EXIT_OK

    if command == "list-checks":
        for index, name in enumerate(CHECK_ORDER, 1):
            print(f"{index:2}. {name}")
        return EXIT_OK

    if command == "run":
        return _run(rest, _build_parser())

    print(f"error: unknown command '{command}'", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
