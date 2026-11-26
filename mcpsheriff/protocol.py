"""Minimal JSON-RPC clients for MCP: stdio subprocess and Streamable HTTP.

Deliberately stdlib-only. The tool has to work on a clean machine right after
`pip install mcpsheriff`, and a broken MCP server must not be able to crash or
hang the checker.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"
KNOWN_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

STDERR_LIMIT = 8192

# Serialises writes to a server's stdin. The request path and the reply to a
# server-initiated request arrive from different threads, and interleaved
# writes would corrupt the JSON-RPC stream.
_STDIN_LOCK = threading.Lock()

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bnr_hub_[A-Za-z0-9]{8,}\b"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[=:]\s*)[^\s,;\"']+"),
)


def _redact(text: str) -> str:
    """Remove credentials before stderr reaches a report or CI log."""
    for pattern in _SECRET_PATTERNS:

        def replace(match: "re.Match[str]") -> str:
            prefix = match.group(1) if match.groups() else ""
            return f"{prefix}<redacted>"

        text = pattern.sub(replace, text)
    return text


class ProtocolError(RuntimeError):
    """Transport-level failure: process died, timeout, unparsable reply."""


class RpcError(ProtocolError):
    def __init__(self, method: str, error: Dict[str, Any]) -> None:
        self.error = error
        super().__init__(f"{method} returned JSON-RPC error {error.get('code')}: {error.get('message')}")


@dataclass
class Session:
    """What a check needs from a transport."""

    noise: List[str] = field(default_factory=list)
    stderr: List[str] = field(default_factory=list)
    server_info: Dict[str, Any] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    protocol_version: str = ""
    instructions: str = ""

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def stderr_tail(self) -> str:
        return _redact("".join(self.stderr)[-2000:])


class StdioSession(Session):
    def __init__(self, command: List[str], env: Optional[Dict[str, str]] = None) -> None:
        super().__init__()
        self.command = command
        self.env = env
        self._process: Optional[subprocess.Popen] = None
        self._pending: Dict[int, "queue.Queue[Dict[str, Any]]"] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None

    def start(self) -> None:
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self.env,
            )
        except FileNotFoundError as error:
            raise ProtocolError(f"command not found: {self.command[0]}") from error

        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        assert self._process and self._process.stdout
        for raw in self._process.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Protocol violation: anything on stdout must be JSON-RPC.
                self.noise.append(_redact(line[:400]))
                continue
            if not isinstance(message, dict):
                self.noise.append(_redact(line[:400]))
                continue
            message_id = message.get("id")
            if isinstance(message.get("method"), str):
                # A server-initiated request (sampling, roots, ping...). Staying
                # silent makes the server wait forever, which later looks like a
                # hung tool call, so answer with "method not found" explicitly.
                if message_id is not None:
                    self._reply_unsupported(message_id, str(message["method"]))
                continue
            if message_id is None:
                continue
            inbox = self._pending.pop(message_id, None)
            if inbox is not None:
                inbox.put(message)

    def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        total = 0
        for raw in self._process.stderr:
            if total < STDERR_LIMIT:
                self.stderr.append(raw)
                total += len(raw)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
        if self._process is None or self._process.poll() is not None:
            exit_code = self._process.poll() if self._process else "?"
            raise ProtocolError(f"server exited before '{method}' (exit code {exit_code})")

        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
            self._pending[request_id] = inbox
            payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params

            assert self._process.stdin
            try:
                with _STDIN_LOCK:
                    self._process.stdin.write(json.dumps(payload) + "\n")
                    self._process.stdin.flush()
            except (BrokenPipeError, ValueError) as error:
                self._pending.pop(request_id, None)
                raise ProtocolError(f"server closed stdin during '{method}'") from error

        try:
            message = inbox.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(request_id, None)
            raise ProtocolError(f"no reply to '{method}' within {timeout:g}s") from None

        if "error" in message:
            raise RpcError(method, message["error"])
        result = message.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def _reply_unsupported(self, message_id: Any, method: str) -> None:
        if self._process is None or self._process.stdin is None:
            return
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {
                "code": -32601,
                "message": f"mcpsheriff does not implement '{method}'",
            },
        }
        try:
            with _STDIN_LOCK:
                self._process.stdin.write(json.dumps(payload) + "\n")
                self._process.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if self._process is None or self._process.stdin is None:
            raise ProtocolError("server is not running")
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        with _STDIN_LOCK:
            self._process.stdin.write(json.dumps(payload) + "\n")
            self._process.stdin.flush()

    def close(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


class HttpSession(Session):
    """Streamable HTTP transport: one POST per JSON-RPC message."""

    def __init__(self, url: str, timeout: float = 10.0, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__()
        self.extra_headers: Dict[str, str] = dict(headers or {})
        self.url = url
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._next_id = 0

    def _post(self, payload: Dict[str, Any], expect_reply: bool = True) -> Optional[Dict[str, Any]]:
        import urllib.error
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "mcpsheriff",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            # The 2025-06-18 transport requires this header on requests after initialize.
            headers["MCP-Protocol-Version"] = self.protocol_version
        headers.update(self.extra_headers)

        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                content_type = response.headers.get("Content-Type", "")
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            raise ProtocolError(f"HTTP {error.code} from {self.url}: {body[:300]}") from error
        except Exception as error:  # noqa: BLE001 - network stack
            raise ProtocolError(f"cannot reach {self.url}: {type(error).__name__}: {error}") from error

        if not expect_reply:
            return None
        if "text/event-stream" in content_type:
            return self._parse_sse(body, payload.get("id"))
        try:
            return json.loads(body) if body.strip() else None
        except json.JSONDecodeError as error:
            raise ProtocolError(f"response is not JSON and not SSE: {body[:200]}") from error

    def _parse_sse(self, body: str, request_id: Any) -> Dict[str, Any]:
        data_lines: List[str] = []
        for line in body.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
                continue
            if line.strip() == "" and data_lines:
                message = self._decode("".join(data_lines), request_id)
                if message is not None:
                    return message
                data_lines = []
        if data_lines:
            message = self._decode("".join(data_lines), request_id)
            if message is not None:
                return message
        raise ProtocolError("SSE stream ended without a JSON-RPC response")

    @staticmethod
    def _decode(raw: str, request_id: Any) -> Optional[Dict[str, Any]]:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
        return None

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
        self.timeout = timeout
        self._next_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        message = self._post(payload)
        if message is None:
            raise ProtocolError(f"empty reply to '{method}'")
        if "error" in message:
            raise RpcError(method, message["error"])
        result = message.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload, expect_reply=False)

    def close(self) -> None:
        """Best effort: tell the server the session is over (DELETE is optional in the spec)."""
        if not self.session_id:
            return
        import urllib.request

        try:
            request = urllib.request.Request(
                self.url,
                headers={"Mcp-Session-Id": self.session_id, "User-Agent": "mcpsheriff"},
                method="DELETE",
            )
            urllib.request.urlopen(request, timeout=2).close()
        except Exception:  # noqa: BLE001 - optional cleanup
            return
