import json
import queue
import subprocess
import sys
import threading
from collections.abc import Iterator
from io import TextIOWrapper
from typing import Any

import pytest


class Session:
    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "assessment.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.lines: list[str] = []
        self.responses: queue.Queue[str] = queue.Queue()
        self.number = 0

        def read() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.lines.append(line)
                self.responses.put(line)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        result = self.send(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "subprocess-test", "version": "1"},
                },
            }
        )
        assert result["result"]["serverInfo"]["name"] == "customer-assessment"
        self.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def write(self, raw: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(raw + "\n")
        self.process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        return json.loads(self.responses.get(timeout=10))  # type: ignore[no-any-return]

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.write(json.dumps(payload))
        return self.receive()

    def call(self, tool: str, arguments: Any) -> dict[str, Any]:
        self.number += 1
        response = self.send(
            {
                "jsonrpc": "2.0",
                "id": self.number,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        assert response["id"] == self.number
        return response


@pytest.fixture(scope="module")
def session() -> Iterator[Session]:
    session = Session()
    try:
        yield session
    finally:
        assert session.process.stdin is not None
        session.process.stdin.close()
        try:
            session.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            session.process.kill()
            raise
        session.reader.join(timeout=2)
        assert session.process.stderr is not None
        stderr = session.process.stderr.read()
        assert session.process.returncode == 0, stderr
        assert "Processing request" in stderr
        for line in session.lines:
            parsed = json.loads(line)
            assert parsed["jsonrpc"] == "2.0"
            assert "result" in parsed or "error" in parsed
        assert session.responses.empty(), "Unexpected protocol output or notification response"


def test_list_and_both_tools(session: Session) -> None:
    result = session.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
    tools = {tool["name"]: tool for tool in result["result"]["tools"]}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    assert tools["trigger_refund"]["inputSchema"]["additionalProperties"] is False
    assert (
        session.call("get_customer_record", {"customer_id": "CUST-12345"})["result"][
            "structuredContent"
        ]["name"]
        == "Synthetic Customer"
    )
    result = session.call(
        "trigger_refund",
        {"customer_id": "CUST-12345", "amount": 10.1, "reason": "  Duplicate charge  "},
    )
    assert result["result"]["structuredContent"]["amount"] == "10.1"
    assert result["result"]["structuredContent"]["reason"] == "Duplicate charge"
    assert "error" not in session.call(
        "trigger_refund", {"customer_id": "CUST-12345", "amount": 1, "reason": "Valid reason"}
    )
    exact = session.call(
        "trigger_refund",
        {"customer_id": "CUST-12345", "amount": 9007199254740993, "reason": "Valid reason"},
    )
    assert exact["result"]["structuredContent"]["amount"] == "9007199254740993"


@pytest.mark.parametrize(
    "customer_id",
    [
        12345,
        True,
        "cust-12345",
        "CUST-1234",
        "CUST-１２３４５",
        "CUST-12345\n",
        " CUST-12345",
        None,
    ],
)
def test_strict_customer_id(session: Session, customer_id: Any) -> None:
    assert (
        session.call("get_customer_record", {"customer_id": customer_id})["error"]["code"] == -32602
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": 0},
        {"amount": -1},
        {"amount": True},
        {"amount": "10"},
        {"amount": None},
        {"reason": "          "},
        {"reason": " short "},
        {"reason": 1234567890},
        {"unexpected": "field"},
    ],
)
def test_invalid_refund_returns_protocol_error(session: Session, changes: dict[str, Any]) -> None:
    args = {"customer_id": "CUST-12345", "amount": 10.0, "reason": "Duplicate charge", **changes}
    result = session.call("trigger_refund", args)
    assert result["error"]["code"] == -32602
    assert "result" not in result


def test_wire_failures_and_no_notification_side_effects(session: Session) -> None:
    assert isinstance(session.process.stdin, TextIOWrapper)
    session.process.stdin.buffer.write(b"\xff\n")
    session.process.stdin.buffer.flush()
    assert session.receive()["error"]["code"] == -32700
    for raw, code in [
        ("{", -32700),
        ("[]", -32600),
        ('{"jsonrpc":"2.0","id":99,"method":"unknown"}', -32601),
        ('{"jsonrpc":"2.0","id":99,"method":"tools/call","params":[]}', -32602),
        ('{"jsonrpc":"2.0","id":99,"method":"tools/list","params":null}', -32602),
        ('{"jsonrpc":"2.0","id":99,"method":"ping","params":null}', -32602),
        (r'{"jsonrpc":"2.0","id":99,"method":"tools/list","params":{"cursor":"\ud800"}}', -32700),
        (
            '{"jsonrpc":"2.0","id":99,"method":"tools/call","params":'
            '{"name":"trigger_refund","arguments":{"customer_id":"CUST-12345",'
            '"amount":1e309,"reason":"Valid reason"}}}',
            -32602,
        ),
    ]:
        session.write(raw)
        assert session.receive()["error"]["code"] == code
    assert session.call("missing_tool", {})["error"]["code"] == -32602
    session.write('{"jsonrpc":"2.0","method":"tools/call","params":{"name":"trigger_refund"}}')
    assert "result" in session.call("get_customer_record", {"customer_id": "CUST-12345"})
