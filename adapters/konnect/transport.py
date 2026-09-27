"""Explicit newline-delimited stdio MCP transport for installed Konnect."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ...debug import describe, emit, span
from ...backend_config import backend_evidence


TOOLSETS = [
    "project",
    "sch_components",
    "sch_wiring",
    "sch_analysis",
    "sch_batch",
    "sch_export",
    "pcb_board",
    "pcb_components",
    "pcb_routing",
    "verification",
    "library",
]


class StdioKonnectTransport:
    def __init__(self, executable: str | Path, runtime_directory: str | Path, *, heartbeat_seconds: float = 5.0,
                 ipc_address: str | None = None):
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.heartbeat_seconds = heartbeat_seconds
        self.ipc_address = ipc_address
        environment = os.environ.copy()
        # Native IPC boundaries reveal which request stalled without logging
        # electrical payloads or changing timeout/retry behavior. Child-local;
        # preserve any explicitly configured Rust log filter.
        environment.setdefault("RUST_LOG", "konnect_ipc=debug")
        if ipc_address is not None:
            if not ipc_address.startswith("ipc://") or not ipc_address[6:]:
                raise ValueError("ipc_address must be a local ipc:// endpoint")
            environment["KICAD_API_SOCKET"] = ipc_address
        self.executable = Path(executable).resolve()
        self.runtime_directory = Path(runtime_directory).resolve()
        if not self.executable.is_file():
            raise FileNotFoundError(self.executable)
        self.binary_evidence = backend_evidence(executable=self.executable)
        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [str(self.executable)],
            cwd=self.runtime_directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        self.next_id = 1
        self.initialized = False
        self._responses: queue.Queue = queue.Queue()
        self._stderr_tail = bytearray()
        self._stdout_reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_reader.start()
        self._stderr_reader.start()
        emit(event="STARTED", scope="konnect.process", pid=self.process.pid,
             executable=str(self.executable), runtime_directory=str(self.runtime_directory),
             ipc_address=ipc_address,
             stderr_log=str(self.runtime_directory / "konnect_stderr.log"))

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for payload in iter(self.process.stdout.readline, b""):
                self._responses.put(payload)
        except Exception as exc:
            self._responses.put(exc)
        finally:
            self._responses.put(None)

    def _read_stderr(self) -> None:
        # Drain concurrently: a full stderr pipe otherwise deadlocks a tool call.
        assert self.process.stderr is not None
        with (self.runtime_directory / "konnect_stderr.log").open("ab", buffering=0) as stream:
            while True:
                chunk = self.process.stderr.read1(4096)
                if not chunk:
                    break
                stream.write(chunk)
                self._stderr_tail.extend(chunk)
                del self._stderr_tail[:-65536]

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        label = params.get("name", method)
        with span(scope="konnect.request", request_id=request_id, tool=label,
                  **describe(values=params.get("arguments", {}))):
            return self._exchange(request_id=request_id, method=method, params=params)

    def _exchange(self, *, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        self.process.stdin.flush()
        started = time.monotonic()
        next_heartbeat = started + self.heartbeat_seconds
        while True:
            now = time.monotonic()
            if now >= next_heartbeat:
                emit(event="WAITING", scope="konnect.request", request_id=request_id,
                     tool=params.get("name", method), elapsed_s=round(now - started, 1),
                     pid=self.process.pid, stderr_log=str(self.runtime_directory / "konnect_stderr.log"))
                next_heartbeat = now + self.heartbeat_seconds
            try:
                payload = self._responses.get(timeout=max(0.001, next_heartbeat - now))
            except queue.Empty:
                continue
            if payload is None:
                raise RuntimeError("Konnect closed stdout while awaiting MCP response")
            if isinstance(payload, Exception):
                raise payload
            response = json.loads(payload.decode("utf-8"))
            if response.get("id") == request_id:
                return response
            emit(event="NOTIFICATION", scope="konnect.request", request_id=request_id,
                 method=response.get("method"), response_id=response.get("id"))

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        self.process.stdin.flush()

    def initialize(self) -> dict[str, Any]:
        response = self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "oomlout-kicad-plan-executor", "version": "1.0"},
            },
        )
        if response.get("error"):
            raise RuntimeError(f"Konnect initialize failed: {response['error']}")
        self._notify("notifications/initialized", {})
        self.call_tool("load_toolset", {"name": TOOLSETS})
        installation = self.call_tool("get_installation_info", {})
        if self.ipc_address is not None:
            # A user Konnect config takes precedence over its environment. Never
            # silently use that other endpoint after selecting an exact board.
            data = installation.get("structuredContent")
            if not isinstance(data, dict):
                data = next((json.loads(item["text"]) for item in installation.get("content", [])
                             if item.get("type") == "text"), {})
            effective = data.get("ipc", {}).get("endpoint")
            if effective != self.ipc_address:
                raise RuntimeError(f"Konnect IPC endpoint override rejected: requested {self.ipc_address!r}, effective {effective!r}")
        self.initialized = True
        return {
            "protocol": response.get("result", {}),
            "executable": str(self.executable),
            "binary_evidence": self.binary_evidence,
            "runtime_directory": str(self.runtime_directory),
            "installation": installation,
            "toolsets": TOOLSETS,
        }

    def list_tools(self) -> dict[str, Any]:
        response = self._request("tools/list", {})
        if response.get("error"):
            raise RuntimeError(f"Konnect tools/list failed: {response['error']}")
        return response.get("result", {})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        response = self._request("tools/call", {"name": name, "arguments": arguments})
        if response.get("error"):
            return {"error": response["error"]}
        return response.get("result", {})

    def shutdown(self) -> dict[str, Any]:
        emit(event="START", scope="konnect.shutdown", pid=self.process.pid)
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            return_code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            return_code = self.process.wait(timeout=10)
        self._stdout_reader.join(timeout=2)
        self._stderr_reader.join(timeout=2)
        stderr = self._stderr_tail.decode("utf-8", errors="replace")
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.process.stderr.close()
        emit(event="END", scope="konnect.shutdown", return_code=return_code)
        return {"return_code": return_code, "stderr": stderr,
                "stderr_log": str(self.runtime_directory / "konnect_stderr.log")}
