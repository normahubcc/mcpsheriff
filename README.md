# mcpsheriff

Protocol conformance checks for [MCP](https://modelcontextprotocol.io) servers.
Nine checks, one command, an exit code you can gate CI on.

```bash
pip install mcpsheriff
mcpsheriff run -- npx -y @normahub/mcp-server
mcpsheriff run --http https://example.com/mcp
```

```
PASS  handshake              protocolVersion=2025-06-18; serverInfo='my-server'/'1.4.0'; capabilities=prompts,resources,tools
PASS  tools.list             8 tool(s): alpha, beta, ...
PASS  tools.schema           all 8 input schemas are consistent
PASS  tools.unknown          unknown tool rejected with isError
PASS  resources.list         2 resource(s)
PASS  prompts.list           3 prompt(s)
PASS  errors.recovery        server answered ping after an unknown method
PASS  latency                tools/list answered in 3 ms
PASS  stdout.hygiene         every stdout line is valid JSON-RPC

9 passed, 0 failed, 0 skipped (9 checks)
```

## What it catches

| Check | Failure it detects |
|---|---|
| `handshake` | missing `protocolVersion`, `serverInfo`, or `capabilities` that is not an object |
| `tools.list` | names outside `^[a-z0-9_-]{1,64}$`, duplicates, empty descriptions, `inputSchema` that is not an object |
| `tools.schema` | `required` entries missing from `properties`; properties without a type |
| `tools.unknown` | calling a tool that does not exist crashes the server instead of returning `isError` |
| `resources.list` | capability advertised, list empty or malformed (skipped when the capability is absent) |
| `prompts.list` | same, for prompts |
| `errors.recovery` | server stops answering after an unknown method |
| `stdout.hygiene` | anything on stdout that is not JSON-RPC (`print`, `console.log`) |
| `latency` | `tools/list` slower than the budget (informational, severity `low`) |

An empty `capabilities` object is valid: it means the server offers the core
protocol only, and the handshake passes with a note.

## Exit codes and `--fail-on`

| Code | Meaning |
|---|---|
| 0 | no blocking failures |
| 1 | at least one blocking failure |
| 2 | could not start: bad arguments, missing command, transport error |

By default only `critical` and `high` failures block. `--fail-on medium` and
`--fail-on low` make the gate stricter; a `low` failure alone no longer fails CI.

## Auth and custom headers

```bash
mcpsheriff run --http https://example.com/mcp --header "Authorization: Bearer $TOKEN"
```

Headers are sent with every request after `initialize`. They are never echoed;
secret values in diagnostics are replaced with `<redacted>`.

## Schema drift

```bash
mcpsheriff run -- npx -y @normahub/mcp-server --snapshot mcp.json
mcpsheriff run -- npx -y @normahub/mcp-server --snapshot mcp.json --update-snapshot
```

The baseline records protocol version, tool names, descriptions and input
schemas, resource and prompt lists. A later run fails when the contract
changes, which is what you want before publishing a new server version.

## Reports

```bash
mcpsheriff run --junit junit.xml --json report.json -- npx -y @normahub/mcp-server
mcpsheriff run --only handshake,tools.list --quiet -- npx -y @normahub/mcp-server
```

`--junit` produces JUnit XML for CI; `--json` writes the same results as JSON.
Both pass through the same secret redaction as the terminal output.

### GitHub Actions

```yaml
- run: pip install mcpsheriff
- run: mcpsheriff run --junit mcpsheriff.xml -- npx -y @your-org/your-mcp-server
- if: always()
  uses: actions/upload-artifact@v4
  with:
    name: mcpsheriff
    path: mcpsheriff.xml
```

## Conformance badge

```bash
mcpsheriff run --json report.json -- npx -y @your-org/your-mcp-server
mcpsheriff-badge --report report.json --out badge.svg
```

This writes the SVG and prints a ready Markdown snippet:

```markdown
[![MCP conformance 9/9](badge.svg)](https://normahub.cc/mcpsheriff)
```

The score is computed from the report, so a badge cannot claim a run the
server did not pass. Copy the SVG next to your README and paste the snippet.
`--label` changes the left-hand text, `--link` changes the target.

## Scope

What it does not do, on purpose:

- it does not judge the quality of model output — that is not deterministic;
- it does not test OAuth, notifications, progress, cancellation, logging or
  pagination; those need a client that stays connected for a session;
- `isError: true` and a JSON-RPC error are both accepted for an unknown tool,
  because the specification allows either;
- HTTP transport supports Streamable HTTP only, not the legacy HTTP+SSE pair;
- on Windows, commands that are shell built-ins (`npx`) need `cmd /c`.

## Requirements

Python 3.9+, no third-party dependencies. `pip install mcpsheriff`.

## Development

```bash
pip install -e ".[dev]"
pytest -q          # 28 tests
ruff check .
python -m build
twine check dist/*
```

## License

MIT. See [LICENSE](LICENSE).
