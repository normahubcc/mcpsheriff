"""End-to-end tests: every check is proven to fail against a server that is wrong."""

from __future__ import annotations

import json
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mcpsheriff import __version__
from mcpsheriff.cli import EXIT_ERROR, EXIT_FAIL, EXIT_OK, main

SERVER = str(Path(__file__).with_name("servers.py"))
BASE = [sys.executable, SERVER]


def run(monkeypatch, capsys, args, mode="good"):
    monkeypatch.setenv("MCPSHERIFF_FAKE_MODE", mode)
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_version():
    parts = __version__.split(".")
    assert len(parts) == 3 and all(part.isdigit() for part in parts)


def test_good_server_passes_every_check(monkeypatch, capsys):
    code, output = run(monkeypatch, capsys, ["run", "--", *BASE])
    assert code == EXIT_OK
    assert "9 passed, 0 failed, 0 skipped" in output
    assert "FAIL" not in output


def test_noisy_stdout_is_a_critical_failure(monkeypatch, capsys):
    code, output = run(monkeypatch, capsys, ["run", "--", *BASE], mode="noisy")
    assert code == EXIT_FAIL
    assert "FAIL  stdout.hygiene" in output
    assert "DEBUG: starting fake server" in output


def test_bad_schema_is_reported(monkeypatch, capsys):
    code, output = run(monkeypatch, capsys, ["run", "--", *BASE], mode="bad_schema")
    assert code == EXIT_FAIL
    assert "FAIL  tools.schema" in output
    assert "missing_property" in output


def test_server_that_crashes_on_unknown_tool_fails_the_check(monkeypatch, capsys):
    code, output = run(
        monkeypatch, capsys, ["run", "--timeout", "5", "--", *BASE], mode="crash_unknown"
    )
    assert code == EXIT_FAIL
    assert "FAIL  tools.unknown" in output


def test_missing_capabilities_are_skipped_not_failed(monkeypatch, capsys):
    code, output = run(monkeypatch, capsys, ["run", "--", *BASE], mode="no_resources")
    assert code == EXIT_OK
    assert "2 skipped" in output
    assert "SKIP  resources.list" in output


def test_only_selection_and_unknown_check(monkeypatch, capsys):
    code, output = run(monkeypatch, capsys, ["run", "--only", "handshake", "--", *BASE])
    assert code == EXIT_OK
    assert "1 passed" in output

    code, output = run(monkeypatch, capsys, ["run", "--only", "nope", "--", *BASE])
    assert code == EXIT_ERROR
    assert "unknown check" in output


def test_snapshot_write_then_drift(monkeypatch, capsys, tmp_path):
    snapshot = tmp_path / "mcp.snapshot.json"

    code, output = run(
        monkeypatch, capsys, ["run", "--snapshot", str(snapshot), "--update-snapshot", "--", *BASE]
    )
    assert code == EXIT_OK
    assert snapshot.exists()
    assert "tools" in json.loads(snapshot.read_text(encoding="utf-8"))

    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["tools"]["ghost"] = {"description": "tool added in another release", "inputSchema": {"type": "object"}}
    snapshot.write_text(json.dumps(data), encoding="utf-8")

    code, output = run(monkeypatch, capsys, ["run", "--snapshot", str(snapshot), "--", *BASE])
    assert code == EXIT_FAIL
    assert "snapshot" in output
    assert "ghost" in output


def test_junit_and_json_reports(monkeypatch, capsys, tmp_path):
    junit = tmp_path / "report.xml"
    payload = tmp_path / "report.json"

    code, _ = run(
        monkeypatch, capsys, ["run", "--junit", str(junit), "--json", str(payload), "--", *BASE]
    )
    assert code == EXIT_OK

    suite = ET.parse(junit).getroot()
    assert suite.tag == "testsuite"
    assert suite.get("tests") == "9"
    assert suite.get("failures") == "0"

    parsed = json.loads(payload.read_text(encoding="utf-8"))
    assert parsed["summary"] == {"total": 9, "passed": 9, "failed": 0, "skipped": 0}
    assert {check["status"] for check in parsed["checks"]} == {"pass"}


def test_junit_marks_failures(monkeypatch, capsys, tmp_path):
    junit = tmp_path / "fail.xml"
    code, _ = run(monkeypatch, capsys, ["run", "--junit", str(junit), "--", *BASE], mode="bad_schema")
    assert code == EXIT_FAIL

    suite = ET.parse(junit).getroot()
    assert int(suite.get("failures")) >= 1
    failed_cases = [case.get("name") for case in suite.findall("./testcase") if case.find("failure") is not None]
    assert "tools.schema" in failed_cases


def test_missing_command_exits_2(monkeypatch, capsys):
    code, output = run(monkeypatch, capsys, ["run", "--", "definitely-not-a-real-command-9f3a"])
    assert code == EXIT_ERROR
    assert "command not found" in output


def test_help_and_list_checks(capsys):
    assert main([]) == EXIT_OK
    assert "mcpsheriff run" in capsys.readouterr().out

    assert main(["list-checks"]) == EXIT_OK
    listed = capsys.readouterr().out
    for name in ("handshake", "tools.list", "stdout.hygiene", "latency"):
        assert name in listed


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length) or b"{}")
        from servers import respond  # tests directory is on sys.path when pytest runs

        response = respond(message)
        body = json.dumps(response or {}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", "test-session")
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # silence the test server
        pass


@pytest.fixture()
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()


def test_http_transport(http_server, capsys, monkeypatch):
    code = main(["run", "--http", http_server])
    output = capsys.readouterr().out
    assert code == EXIT_OK
    assert "9 passed" in output
