"""Individual checks for an MCP server.

Each check is a small function that receives the session (already initialized)
and returns ``Result``. Checks never raise: a crash inside a check becomes a
failed result with the exception text, because a tester that dies on the first
bad server is useless.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .protocol import KNOWN_VERSIONS, PROTOCOL_VERSION, ProtocolError, RpcError, Session

TOOL_NAME = re.compile(r"^[a-z0-9_-]{1,64}$")
LATENCY_BUDGET_MS = 5000

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def blocking_failures(results, threshold: str = "high"):
    """Failures that decide the exit code: severity at or above the threshold."""
    limit = SEVERITY_ORDER.get(threshold, SEVERITY_ORDER["high"])
    return [
        result
        for result in results
        if not result.ok and not result.skipped and SEVERITY_ORDER.get(result.severity, 1) <= limit
    ]


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "high"
    skipped: bool = False

    @property
    def status(self) -> str:
        if self.skipped:
            return "skip"
        return "pass" if self.ok else "FAIL"


@dataclass
class Context:
    """Everything a check may need beyond the live session."""

    session: Session
    timeout: float = 10.0
    latency_budget_ms: int = LATENCY_BUDGET_MS
    tools: List[Dict[str, Any]] = field(default_factory=list)
    resources: List[Dict[str, Any]] = field(default_factory=list)
    prompts: List[Dict[str, Any]] = field(default_factory=list)


def _fail(name: str, detail: str, severity: str = "high") -> Result:
    return Result(name=name, ok=False, detail=detail, severity=severity)


def _pass(name: str, detail: str = "", severity: str = "high") -> Result:
    return Result(name=name, ok=True, detail=detail, severity=severity)


# --------------------------------------------------------------------------
# handshake


def check_handshake(ctx: Context) -> Result:
    session = ctx.session
    version = session.protocol_version
    if not version:
        return _fail("handshake", "initialize returned no protocolVersion", "critical")

    info = session.server_info or {}
    missing = [key for key in ("name", "version") if not info.get(key)]
    caps = session.capabilities

    notes = [f"protocolVersion={version}", f"serverInfo={info.get('name')!r}/{info.get('version')!r}"]
    if not isinstance(caps, dict):
        return _fail("handshake", "; ".join(notes + ["capabilities is not an object"]), "high")
    if caps:
        notes.append(f"capabilities={','.join(sorted(caps))}")
    else:
        # An empty object is valid: the server offers the core protocol only.
        notes.append("capabilities={} (core protocol only)")

    if missing:
        return _fail("handshake", "; ".join(notes + [f"serverInfo missing {missing}"]), "medium")
    if version not in KNOWN_VERSIONS:
        return Result(
            "handshake",
            ok=True,
            detail="; ".join(notes + [f"unknown protocol version (known: {', '.join(KNOWN_VERSIONS)})"]),
            severity="medium",
        )
    return _pass("handshake", "; ".join(notes))


# --------------------------------------------------------------------------
# tools


def check_tools_list(ctx: Context) -> Result:
    if "tools" not in (ctx.session.capabilities or {}):
        return Result("tools.list", ok=True, skipped=True, detail="server does not advertise tools")
    try:
        response = ctx.session.request("tools/list", {}, timeout=ctx.timeout)
    except RpcError as error:
        return _fail("tools.list", f"tools/list returned an error: {error}", "critical")
    tools = response.get("tools")
    if not isinstance(tools, list):
        return _fail("tools.list", "tools/list result has no 'tools' array", "critical")
    if not tools:
        return _fail("tools.list", "server advertises tools but tools/list is empty", "high")

    ctx.tools = [tool for tool in tools if isinstance(tool, dict)]
    problems: List[str] = []
    names: List[str] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            problems.append(f"entry #{index} is not an object")
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not TOOL_NAME.match(name):
            problems.append(f"bad name {name!r} (must match {TOOL_NAME.pattern})")
        else:
            names.append(name)
        if not isinstance(tool.get("description"), str) or not tool["description"].strip():
            problems.append(f"{name or index}: empty description")
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            problems.append(f"{name or index}: inputSchema must be an object with type=object")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        problems.append(f"duplicate names: {', '.join(duplicates)}")

    detail = f"{len(tools)} tools; names: {', '.join(names[:8])}" + ("…" if len(names) > 8 else "")
    if problems:
        return _fail("tools.list", "; ".join(problems[:6]), "high")
    return _pass("tools.list", detail)


def check_tool_schemas(ctx: Context) -> Result:
    tools = [tool for tool in ctx.tools if isinstance(tool.get("inputSchema"), dict)]
    if not tools:
        return Result("tools.schema", ok=True, skipped=True, detail="no tool schemas to validate")
    problems: List[str] = []
    for tool in tools:
        name = tool.get("name")
        schema = tool["inputSchema"]
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            problems.append(f"{name}: inputSchema.properties must be an object")
            continue
        required = schema.get("required", [])
        if not isinstance(required, list):
            problems.append(f"{name}: inputSchema.required must be an array")
            continue
        unknown = [key for key in required if key not in properties]
        if unknown:
            problems.append(f"{name}: required lists undeclared properties {unknown}")
        for prop, spec in properties.items():
            if not isinstance(spec, dict):
                problems.append(f"{name}.{prop}: property schema must be an object")
            elif "type" not in spec and not any(key in spec for key in ("anyOf", "oneOf", "allOf", "$ref")):
                problems.append(f"{name}.{prop}: property has no type")
    if problems:
        return _fail("tools.schema", "; ".join(problems[:6]), "high")
    return _pass("tools.schema", f"{len(tools)} schemas look consistent")


def check_unknown_tool(ctx: Context) -> Result:
    if not ctx.tools:
        return Result("tools.unknown", ok=True, skipped=True, detail="no tools to test against")
    bogus = "mcpsheriff_does_not_exist_9f3a"
    try:
        response = ctx.session.request(
            "tools/call", {"name": bogus, "arguments": {}}, timeout=ctx.timeout
        )
    except RpcError as error:
        return _pass("tools.unknown", f"rejected with JSON-RPC error ({error.error.get('code')})")
    except ProtocolError as error:
        return _fail("tools.unknown", f"server did not answer a call to an unknown tool: {error}", "critical")
    if response.get("isError") is True:
        return _pass("tools.unknown", "unknown tool returned isError=true")
    return _fail(
        "tools.unknown",
        "calling an unknown tool neither returned isError=true nor a JSON-RPC error",
        "high",
    )


# --------------------------------------------------------------------------
# resources / prompts


def _check_list(ctx: Context, capability: str, method: str, key: str, required_keys: List[str]) -> Result:
    label = f"{capability}.list"
    if capability not in (ctx.session.capabilities or {}):
        return Result(label, ok=True, skipped=True, detail=f"server does not advertise {capability}")
    try:
        response = ctx.session.request(method, {}, timeout=ctx.timeout)
    except RpcError as error:
        return _fail(label, f"{method} returned an error: {error}", "high")
    items = response.get(key)
    if not isinstance(items, list):
        return _fail(label, f"{method} result has no '{key}' array", "high")
    if not items:
        return _fail(label, f"server advertises {capability} but {method} is empty", "medium")
    problems: List[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"entry #{index} is not an object")
            continue
        for required in required_keys:
            if not item.get(required):
                problems.append(f"entry #{index}: missing '{required}'")
    if problems:
        return _fail(label, "; ".join(problems[:6]), "medium")
    return _pass(label, f"{len(items)} entries")


def check_resources(ctx: Context) -> Result:
    result = _check_list(ctx, "resources", "resources/list", "resources", ["uri", "name"])
    if result.ok and not result.skipped:
        try:
            ctx.resources = ctx.session.request("resources/list", {}, timeout=ctx.timeout)["resources"]
        except ProtocolError:
            pass
    return result


def check_prompts(ctx: Context) -> Result:
    result = _check_list(ctx, "prompts", "prompts/list", "prompts", ["name"])
    if result.ok and not result.skipped:
        try:
            ctx.prompts = ctx.session.request("prompts/list", {}, timeout=ctx.timeout)["prompts"]
        except ProtocolError:
            pass
    return result


# --------------------------------------------------------------------------
# robustness


def check_stdout_hygiene(ctx: Context) -> Result:
    noise = list(ctx.session.noise)
    if not noise:
        return _pass("stdout.hygiene", "every stdout line was valid JSON-RPC")
    sample = "; ".join(noise[:3])
    return _fail(
        "stdout.hygiene",
        f"{len(noise)} non-JSON line(s) on stdout, e.g. {sample}",
        "critical",
    )


def check_survives_bad_request(ctx: Context) -> Result:
    try:
        try:
            ctx.session.request("mcpsheriff/unknown_method", {}, timeout=ctx.timeout)
        except RpcError:
            pass  # a JSON-RPC error is the correct answer here
    except ProtocolError as error:
        return _fail("errors.recovery", f"server stopped answering after an unknown method: {error}", "critical")

    try:
        ctx.session.request("ping", {}, timeout=ctx.timeout)
    except ProtocolError as error:
        return _fail("errors.recovery", f"no reply to ping after an unknown method: {error}", "high")
    except RpcError:
        pass  # ping may be unimplemented; an error still proves the process lives
    return _pass("errors.recovery", "still responding after an unknown method")


def check_latency(ctx: Context) -> Result:
    started = time.monotonic()
    try:
        ctx.session.request("tools/list", {}, timeout=max(ctx.timeout, 30.0))
    except (ProtocolError, RpcError) as error:
        return _fail("latency", f"tools/list failed: {error}", "low")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if elapsed_ms > ctx.latency_budget_ms:
        return Result(
            "latency",
            ok=False,
            detail=f"tools/list took {elapsed_ms} ms (budget {ctx.latency_budget_ms} ms)",
            severity="low",
        )
    return _pass("latency", f"tools/list in {elapsed_ms} ms")


# --------------------------------------------------------------------------
# schema drift


def snapshot_of(ctx: Context) -> Dict[str, Any]:
    def summarise(items: List[Dict[str, Any]], keys: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                out[item["name"]] = {key: item.get(key) for key in keys}
        return out

    return {
        "protocolVersion": ctx.session.protocol_version,
        "tools": summarise(ctx.tools, ["description", "inputSchema"]),
        "resources": summarise(ctx.resources, ["uri", "description"]),
        "prompts": summarise(ctx.prompts, ["description", "arguments"]),
    }


def diff_snapshot(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    changes: List[str] = []
    if old.get("protocolVersion") != new.get("protocolVersion"):
        changes.append(f"protocolVersion {old.get('protocolVersion')} -> {new.get('protocolVersion')}")
    for section in ("tools", "resources", "prompts"):
        before = old.get(section) or {}
        after = new.get(section) or {}
        for name in sorted(set(after) - set(before)):
            changes.append(f"{section}: added {name}")
        for name in sorted(set(before) - set(after)):
            changes.append(f"{section}: removed {name}")
        for name in sorted(set(before) & set(after)):
            if before[name] != after[name]:
                changes.append(f"{section}: changed {name}")
    return changes


def check_snapshot(ctx: Context, snapshot_path: str, update: bool = False) -> Optional[Result]:
    """Returns None when there is nothing to compare against and no --update."""
    current = snapshot_of(ctx)
    try:
        with open(snapshot_path, "r", encoding="utf-8") as handle:
            previous = json.load(handle)
    except FileNotFoundError:
        if update:
            _write_snapshot(snapshot_path, current)
            return _pass("schema.snapshot", f"baseline written to {snapshot_path}")
        return Result(
            "schema.snapshot",
            ok=False,
            detail=f"no snapshot at {snapshot_path}; run again with --update-snapshot to create it",
            severity="medium",
        )
    except (OSError, ValueError) as error:
        return _fail("schema.snapshot", f"cannot read snapshot {snapshot_path}: {error}", "medium")

    changes = diff_snapshot(previous, current)
    if update:
        _write_snapshot(snapshot_path, current)
        if changes:
            return _pass("schema.snapshot", "baseline updated; changes: " + "; ".join(changes[:8]))
        return _pass("schema.snapshot", f"baseline unchanged ({snapshot_path})")
    if changes:
        return _fail("schema.snapshot", "schema drift: " + "; ".join(changes[:8]), "high")
    return _pass("schema.snapshot", f"no drift against {snapshot_path}")


def _write_snapshot(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


# --------------------------------------------------------------------------
# registry

CHECK_ORDER = [
    "handshake",
    "tools.list",
    "tools.schema",
    "tools.unknown",
    "resources.list",
    "prompts.list",
    "errors.recovery",
    "latency",
    "stdout.hygiene",
]

CHECK_FUNCTIONS = {
    "handshake": check_handshake,
    "tools.list": check_tools_list,
    "tools.schema": check_tool_schemas,
    "tools.unknown": check_unknown_tool,
    "resources.list": check_resources,
    "prompts.list": check_prompts,
    "stdout.hygiene": check_stdout_hygiene,
    "errors.recovery": check_survives_bad_request,
    "latency": check_latency,
}


# A check that inspects data collected by another check.
_PREREQUISITES = {"tools.schema": ("tools.list",)}


def run_checks(ctx: Context, only: Optional[List[str]] = None) -> List[Result]:
    """Runs the suite in order. `only` keeps the listed checks (snapshot excluded)."""
    if only:
        only = list(only)
        unknown = [name for name in only if name not in CHECK_FUNCTIONS]
        if unknown:
            raise ValueError(f"unknown check(s): {', '.join(unknown)}; run `mcpsheriff list-checks`")
        # A selected check may read data another check collects; run the
        # prerequisite too, otherwise `--only tools.schema` skips on every server.
        for name in list(only):
            for prerequisite in _PREREQUISITES.get(name, ()):
                if prerequisite not in only:
                    only.append(prerequisite)
    results: List[Result] = []
    for name in CHECK_ORDER:
        if only and name not in only:
            continue
        function = CHECK_FUNCTIONS[name]
        try:
            results.append(function(ctx))
        except Exception as error:  # noqa: BLE001 - a check must never kill the run
            results.append(_fail(name, f"check crashed: {type(error).__name__}: {error}", "critical"))
    return results


def initialize(session: Session, timeout: float = 10.0, client_version: str = "0.1.0") -> None:
    """Performs the MCP handshake and stores the negotiated values on the session."""
    response = session.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcpsheriff", "version": client_version},
        },
        timeout=timeout,
    )
    session.protocol_version = str(response.get("protocolVersion") or "")
    session.capabilities = response.get("capabilities") or {}
    session.server_info = response.get("serverInfo") or {}
    session.instructions = str(response.get("instructions") or "")
    session.notify("notifications/initialized")
