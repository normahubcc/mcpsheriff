"""Fake MCP servers used by the tests.

The point is to exercise mcpsheriff against servers that are wrong in specific
ways, so every check is proven to fail when it should. No dependencies: the
server speaks newline-delimited JSON-RPC on stdin/stdout.
"""

from __future__ import annotations

import json
import os
import sys
import time

PROTOCOL = "2025-06-18"

TOOL_ECHO = {
    "name": "echo",
    "description": "Echo the given text back.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "text to echo"}},
        "required": ["text"],
    },
}

TOOL_BAD_SCHEMA = {
    "name": "broken",
    "description": "Tool whose schema cannot be satisfied.",
    "inputSchema": {
        "type": "object",
        "properties": {"ok": {"description": "no type here"}},
        "required": ["missing_property"],
    },
}

RESOURCE = {"uri": "fake://greeting", "name": "greeting", "description": "A greeting", "mimeType": "text/plain"}
PROMPT = {
    "name": "hello",
    "description": "Say hello",
    "arguments": [{"name": "name", "description": "who", "required": True}],
}


def respond(message, mode="good"):
    """Returns the JSON-RPC response dict for one request, or None for a notification."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None:
        return None  # notification

    if method == "initialize":
        capabilities = {"tools": {}}
        if mode != "no_resources":
            capabilities["resources"] = {}
            capabilities["prompts"] = {}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": capabilities,
                "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
                "instructions": "fake server for tests",
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        if mode == "slow":
            time.sleep(1.5)
        tools = [TOOL_ECHO]
        if mode == "bad_schema":
            tools.append(TOOL_BAD_SCHEMA)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}

    if method == "tools/call":
        name = params.get("name")
        if name == "echo":
            text = (params.get("arguments") or {}).get("text", "")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text or "ok"}], "isError": False},
            }
        if mode == "crash_unknown":
            sys.exit(1)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            },
        }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": [RESOURCE]}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": [PROMPT]}}

    if mode == "ignore_unknown" and method != "ping":
        return None

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    mode = os.environ.get("MCPSHERIFF_FAKE_MODE", "good")
    if mode == "noisy":
        print("DEBUG: starting fake server")  # deliberately pollutes stdout
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = respond(message, mode=mode)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
