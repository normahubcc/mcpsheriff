"""Regression tests for the 0.1.1 hardening: redaction, check order, server requests."""

import sys

from mcpsheriff.checks import CHECK_ORDER
from mcpsheriff.protocol import StdioSession, _redact

# A server that behaves like a real one asking the client for sampling during
# initialization. If the client stays silent the server never answers
# initialize, so a working reply path is what makes this test pass quickly.
SERVER_REQUESTS_SAMPLING = r"""
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        request = {"jsonrpc": "2.0", "id": 900, "method": "sampling/createMessage", "params": {}}
        sys.stdout.write(json.dumps(request) + "\n")
        sys.stdout.flush()
        reply = None
        for follow_up in sys.stdin:
            candidate = json.loads(follow_up)
            if candidate.get("id") == 900:
                reply = candidate
                break
        if reply is None or "error" not in reply:
            result = {"jsonrpc": "2.0", "id": message["id"],
                      "error": {"code": -1, "message": "client did not reject sampling"}}
        else:
            result = {"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "sampling-server", "version": "1.0"}}}
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
"""


def test_redact_hides_common_secret_shapes():
    samples = [
        "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz123456",
        "export NORMAHUB= nr_hub_abcdef123456",
        "npm token npm_abcdefghijklmnopqrstuvwxyz01",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "api_key=super-secret-value token: another-secret",
    ]
    for sample in samples:
        cleaned = _redact(sample)
        assert "<redacted>" in cleaned, sample
    assert "sk-abcdef" not in _redact("Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz123456")
    assert "npm_abcdef" not in _redact("npm token npm_abcdefghijklmnopqrstuvwxyz01")


def test_redact_keeps_ordinary_text():
    assert _redact("listen on port 3001") == "listen on port 3001"


def test_stdout_hygiene_runs_last():
    assert CHECK_ORDER[-1] == "stdout.hygiene"


def test_client_answers_server_request_instead_of_hanging():
    session = StdioSession([sys.executable, "-c", SERVER_REQUESTS_SAMPLING])
    session.start()
    try:
        result = session.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
            timeout=5.0,
        )
    finally:
        session.close()
    assert result["serverInfo"]["name"] == "sampling-server"



def test_blocking_failures_respects_severity_threshold():
    from mcpsheriff.checks import Result, blocking_failures

    results = [
        Result("low.check", False, "slow", "low"),
        Result("critical.check", False, "broken", "critical"),
        Result("fine.check", True, "ok"),
    ]
    strict = blocking_failures(results, "high")
    assert [r.name for r in strict] == ["critical.check"]
    loose = blocking_failures(results, "low")
    assert [r.name for r in loose] == ["low.check", "critical.check"]


def test_parse_headers_accepts_and_rejects():
    import pytest

    from mcpsheriff.cli import _parse_headers

    assert _parse_headers(["Authorization: Bearer abc", "X-Env: staging"]) == {
        "Authorization": "Bearer abc",
        "X-Env": "staging",
    }
    assert _parse_headers([]) == {}
    with pytest.raises(ValueError):
        _parse_headers(["no-colon-here"])


def test_junit_report_redacts_secrets(tmp_path):
    from mcpsheriff.checks import Result
    from mcpsheriff.report import write_junit

    target = tmp_path / "junit.xml"
    secret = "sk-abcdefghijklmnop1234567890"
    write_junit([Result("tools.list", False, f"Authorization: Bearer {secret}", "high")], str(target))
    import xml.etree.ElementTree as ET

    xml = target.read_text(encoding="utf-8")
    assert secret not in xml
    # The XML writer escapes < and >, so compare the parsed attribute, not the raw text.
    message = ET.parse(str(target)).getroot().find("./testcase/failure").get("message")
    assert message == "Authorization: Bearer <redacted>"


def test_only_schema_pulls_in_tools_list():
    """`--only tools.schema` must not silently skip because tools.list did not run."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=str(root), MCPSHERIFF_FAKE_MODE="bad_schema")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcpsheriff.cli",
            "run",
            "--quiet",
            "--only",
            "tools.schema",
            "--",
            sys.executable,
            "tests/servers.py",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "1 failed" in completed.stdout, output
